"""NIOS Core v3.0 -- Self-Healing DataGuard (``sec_006`` Stages 1-3, Stage 5 fast path).

Responsibilities
----------------
STAGE 1  dual-vendor ingestion          ``DualVendorBar`` + the harvested
                                        ``tradingagents/router.py`` bridge
STAGE 2  quarantine                     lag / cross-vendor / split gates with a
                                        persistent append-only quarantine log
STAGE 3  PIT and corporate actions      security master, split and dividend
                                        adjustment, historical PIT FX, and the
                                        SEC EDGAR ``knowledgeTime <= asOfTime``
                                        guard wrapping the harvested
                                        ``tradingagents/sec_edgar.py``
STAGE 5  orderflow normalisation        5-second rolling ``ExecutedFlowTick``
                                        frames written into Ring 1's
                                        ``core.shm_ipc.OfiRingBuffer`` in
                                        ``< 0.05 ms`` per frame

Design constraints
------------------
* No credential, no exchange socket and no broker import exists in this module:
  Ring 2 only *reads* already-fetched vendor payloads (``sec_004``).
* Every rejection is recorded before the caller sees it, so a dropped tick is
  never silently lost (``sec_006``).
* Gate precedence is explicit and ordered: lag -> announced-action split ->
  cross-vendor divergence -> PIT.  A ``>=45%`` single-feed gap is diagnosed as
  an *unannounced split* rather than generic divergence, because that is the
  more specific (and more actionable) failure mode.
* The orderflow fast path allocates nothing but the tick itself, performs no
  I/O, and never touches DuckDB, NATS or the grounding ledger.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence

import duckdb

from core.schemas import (
    FEED_LAG_GATE_MS,
    SPLIT_GUARD_PCT,
    VENDOR_DIVERGENCE_GATE_PCT,
    DualVendorBar,
    ExecutedFlowTick,
    TemporalIntegrityError,
    canonical_pairs_digest,
)

__all__ = [
    "DEFAULT_QUARANTINE_ROOT",
    "QuarantineReason",
    "QuarantineRecord",
    "QuarantineStore",
    "GuardEvent",
    "NormalizedBar",
    "CorporateAction",
    "SecurityMaster",
    "CorporateActionBook",
    "FxPoint",
    "FxBook",
    "FilingRecord",
    "SecEdgarPITGuard",
    "VendorRouterBridge",
    "LoaderBinding",
    "VendorTick",
    "CallableLoader",
    "DualVendorLoaderRouter",
    "OrderflowNormalizer",
    "FlushReport",
    "SelfHealingDataGuard",
    "GuardStats",
    "iso_to_ns",
    "ns_to_iso",
    "percentile",
]

DEFAULT_QUARANTINE_ROOT = Path(__file__).resolve().parents[2] / "data" / "quarantine"

NS_PER_SECOND = 1_000_000_000
FIVE_SECONDS_NS = 5 * NS_PER_SECOND


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def iso_to_ns(value: str | datetime) -> int:
    """UTC nanoseconds for an ISO-8601 date/datetime (naive means UTC)."""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).strip())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(round(moment.timestamp() * NS_PER_SECOND))


def ns_to_iso(value: int) -> str:
    return datetime.fromtimestamp(int(value) / NS_PER_SECOND, tz=timezone.utc).isoformat()


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile; ``0.0`` for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    frac = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


# ---------------------------------------------------------------------------
# Stage 2 -- quarantine log
# ---------------------------------------------------------------------------


class QuarantineReason(str, Enum):
    """Reason codes recorded for every rejected tick/bar (``sec_006``)."""

    FEED_LAG_EXCEEDED = "FEED_LAG_EXCEEDED"
    CROSS_VENDOR_DIVERGENCE = "CROSS_VENDOR_DIVERGENCE"
    UNANNOUNCED_SPLIT_GAP = "UNANNOUNCED_SPLIT_GAP"
    TEMPORAL_LOOKAHEAD = "TEMPORAL_LOOKAHEAD"


@dataclass(slots=True, frozen=True)
class QuarantineRecord:
    """One immutable rejection record."""

    record_id: str
    reason: QuarantineReason
    symbol: str
    p1: float
    p2: float
    feed_lag_ms: float
    divergence_pct: float
    split_gap_pct: float
    valid_time_ns: int
    knowledge_ts_ns: int
    as_of_ns: int
    detail: str
    recorded_at_ns: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason"] = self.reason.value
        return payload


_QUARANTINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine_log (
    record_id       VARCHAR PRIMARY KEY,
    reason          VARCHAR NOT NULL,
    symbol          VARCHAR NOT NULL,
    p1              DOUBLE  NOT NULL,
    p2              DOUBLE  NOT NULL,
    feed_lag_ms     DOUBLE  NOT NULL,
    divergence_pct  DOUBLE  NOT NULL,
    split_gap_pct   DOUBLE  NOT NULL,
    valid_time_ns   BIGINT  NOT NULL,
    knowledge_ts_ns BIGINT  NOT NULL,
    as_of_ns        BIGINT  NOT NULL,
    detail          VARCHAR NOT NULL,
    recorded_at_ns  BIGINT  NOT NULL
)
"""


class QuarantineStore:
    """Append-only DuckDB + JSONL quarantine log.

    There is deliberately **no** update or delete API: a rejected tick stays
    rejected forever, which is what makes a post-mortem of a feed incident
    reproducible.  The JSONL mirror is human-readable and survives a DuckDB
    file being locked by another process.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        jsonl: bool = True,
        db_name: str = "quarantine.duckdb",
    ) -> None:
        self.root = Path(root) if root is not None else DEFAULT_QUARANTINE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / db_name
        self.jsonl_path = self.root / "quarantine.jsonl" if jsonl else None
        self._jsonl_enabled = bool(jsonl)
        self._con = duckdb.connect(str(self.db_path))
        self._con.execute(_QUARANTINE_SCHEMA)
        self._counts: MutableMapping[QuarantineReason, int] = defaultdict(int)
        self._total = 0

    # -- write ------------------------------------------------------------
    def record(
        self,
        reason: QuarantineReason,
        bar: DualVendorBar,
        *,
        as_of_ns: int | None = None,
        detail: str = "",
        recorded_at_ns: int | None = None,
    ) -> QuarantineRecord:
        """Persist one rejection and return its immutable record."""
        reason = QuarantineReason(reason)
        stamp = int(recorded_at_ns if recorded_at_ns is not None else time.time_ns())
        as_of = int(as_of_ns if as_of_ns is not None else bar.knowledge_ts_ns)
        record_id = canonical_pairs_digest(
            [
                ("reason", reason.value),
                ("symbol", bar.symbol),
                ("p1", float(bar.p1)),
                ("p2", float(bar.p2)),
                ("valid_time_ns", int(bar.valid_time_ns)),
                ("knowledge_ts_ns", int(bar.knowledge_ts_ns)),
                ("recorded_at_ns", stamp),
            ]
        )[:32]

        record = QuarantineRecord(
            record_id=record_id,
            reason=reason,
            symbol=str(bar.symbol),
            p1=float(bar.p1),
            p2=float(bar.p2),
            feed_lag_ms=float(bar.feed_lag_ms),
            divergence_pct=float(bar.vendor_divergence_pct),
            split_gap_pct=float(bar.split_gap_pct),
            valid_time_ns=int(bar.valid_time_ns),
            knowledge_ts_ns=int(bar.knowledge_ts_ns),
            as_of_ns=as_of,
            detail=str(detail),
            recorded_at_ns=stamp,
        )

        self._con.execute(
            """
            INSERT INTO quarantine_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record.record_id,
                record.reason.value,
                record.symbol,
                record.p1,
                record.p2,
                record.feed_lag_ms,
                record.divergence_pct,
                record.split_gap_pct,
                record.valid_time_ns,
                record.knowledge_ts_ns,
                record.as_of_ns,
                record.detail,
                record.recorded_at_ns,
            ],
        )
        if self._jsonl_enabled and self.jsonl_path is not None:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        self._counts[reason] += 1
        self._total += 1
        return record

    # -- read -------------------------------------------------------------
    def query(
        self,
        *,
        symbol: str | None = None,
        reason: QuarantineReason | str | None = None,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        sql = ["SELECT * FROM quarantine_log WHERE 1=1"]
        params: list[Any] = []
        if symbol is not None:
            sql.append("AND symbol = ?")
            params.append(str(symbol))
        if reason is not None:
            sql.append("AND reason = ?")
            params.append(QuarantineReason(reason).value)
        sql.append("ORDER BY recorded_at_ns ASC LIMIT ?")
        params.append(int(limit))
        cursor = self._con.execute(" ".join(sql), params)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def by_reason(self) -> dict[str, int]:
        rows = self._con.execute(
            "SELECT reason, COUNT(*) FROM quarantine_log GROUP BY reason"
        ).fetchall()
        return {str(reason): int(count) for reason, count in rows}

    def count(self, reason: QuarantineReason | str | None = None) -> int:
        if reason is None:
            return int(
                self._con.execute("SELECT COUNT(*) FROM quarantine_log").fetchone()[0]
            )
        return int(self.by_reason().get(QuarantineReason(reason).value, 0))

    def last(self) -> dict[str, Any] | None:
        rows = self._con.execute(
            "SELECT * FROM quarantine_log ORDER BY recorded_at_ns DESC LIMIT 1"
        ).fetchall()
        if not rows:
            return None
        columns = [description[0] for description in self._con.description]
        return dict(zip(columns, rows[0]))

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "QuarantineStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Stage 3 -- security master, corporate actions, FX
# ---------------------------------------------------------------------------


class SecurityMaster:
    """Canonical security identifiers with vendor-alias resolution."""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}

    def register(self, canonical: str, *aliases: str) -> str:
        symbol = str(canonical).strip().upper()
        if not symbol:
            raise ValueError("canonical symbol must not be empty")
        self._aliases[symbol] = symbol
        for alias in aliases:
            self._aliases[str(alias).strip().upper()] = symbol
        return symbol

    def resolve(self, raw: str) -> str:
        """Resolve a vendor symbol, falling back to a normalized ticker."""
        token = str(raw).strip().upper()
        if token in self._aliases:
            return self._aliases[token]
        cleaned = token.replace("_", "-").replace("/", "-")
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned

    @property
    def size(self) -> int:
        return len(set(self._aliases.values()))


@dataclass(slots=True, frozen=True)
class CorporateAction:
    """A split or cash dividend with its own knowledge timestamp."""

    symbol: str
    kind: str
    effective_ns: int
    knowledge_ts_ns: int
    ratio: float = 1.0
    amount: float = 0.0

    def __post_init__(self) -> None:
        kind = str(self.kind).strip().upper()
        if kind not in ("SPLIT", "DIVIDEND"):
            raise ValueError(f"unsupported corporate action {self.kind!r}")
        object.__setattr__(self, "kind", kind)
        if float(self.ratio) <= 0.0:
            raise ValueError("split ratio must be positive")
        object.__setattr__(self, "ratio", float(self.ratio))
        object.__setattr__(self, "amount", float(self.amount))
        object.__setattr__(self, "effective_ns", int(self.effective_ns))
        object.__setattr__(self, "knowledge_ts_ns", int(self.knowledge_ts_ns))


class CorporateActionBook:
    """PIT-aware split and dividend adjustment factors."""

    def __init__(self) -> None:
        self._actions: dict[str, list[CorporateAction]] = defaultdict(list)

    def register(self, action: CorporateAction) -> None:
        self._actions[str(action.symbol).upper()].append(action)
        self._actions[str(action.symbol).upper()].sort(key=lambda a: a.effective_ns)

    def actions(self, symbol: str) -> tuple[CorporateAction, ...]:
        return tuple(self._actions.get(str(symbol).upper(), ()))

    def adjustment_factor(
        self, symbol: str, valid_time_ns: int, as_of_ns: int
    ) -> float:
        """Cumulative factor applied to a historical price observed at ``valid_time_ns``.

        Only actions already *known* at ``as_of_ns`` may influence the factor;
        an announced-but-unpublished split is invisible (no lookahead).
        """
        factor = 1.0
        for action in self._actions.get(str(symbol).upper(), ()):
            if int(action.knowledge_ts_ns) > int(as_of_ns):
                continue
            if int(action.effective_ns) > int(as_of_ns):
                continue
            if int(valid_time_ns) < int(action.effective_ns):
                if action.kind == "SPLIT":
                    factor *= 1.0 / float(action.ratio)
                else:
                    # Cash dividends are not price-scaled here; the amount is
                    # returned separately by :meth:`dividend_since`.
                    continue
        return factor

    def dividend_since(self, symbol: str, valid_time_ns: int, as_of_ns: int) -> float:
        total = 0.0
        for action in self._actions.get(str(symbol).upper(), ()):
            if action.kind != "DIVIDEND":
                continue
            if int(action.knowledge_ts_ns) > int(as_of_ns):
                continue
            if int(valid_time_ns) <= int(action.effective_ns) <= int(as_of_ns):
                total += float(action.amount)
        return total


@dataclass(slots=True, frozen=True)
class FxPoint:
    base: str
    quote: str
    rate: float
    knowledge_ts_ns: int

    def __post_init__(self) -> None:
        if float(self.rate) <= 0.0:
            raise ValueError("FX rate must be positive")


class FxBook:
    """Point-in-time historical FX conversion (``sec_006`` Stage 3)."""

    def __init__(self) -> None:
        self._points: dict[tuple[str, str], list[FxPoint]] = defaultdict(list)

    def register(self, point: FxPoint) -> None:
        key = (str(point.base).upper(), str(point.quote).upper())
        self._points[key].append(point)
        self._points[key].sort(key=lambda p: p.knowledge_ts_ns)

    def rate(self, base: str, quote: str, as_of_ns: int) -> float:
        base = str(base).upper()
        quote = str(quote).upper()
        if base == quote:
            return 1.0
        candidates = [
            point
            for point in self._points.get((base, quote), ())
            if int(point.knowledge_ts_ns) <= int(as_of_ns)
        ]
        if candidates:
            return float(candidates[-1].rate)
        inverse = [
            point
            for point in self._points.get((quote, base), ())
            if int(point.knowledge_ts_ns) <= int(as_of_ns)
        ]
        if inverse:
            return 1.0 / float(inverse[-1].rate)
        raise KeyError(
            f"no PIT FX rate {base}/{quote} known as of {ns_to_iso(as_of_ns)}"
        )

    def convert(self, amount: float, base: str, quote: str, as_of_ns: int) -> float:
        return float(amount) * self.rate(base, quote, as_of_ns)


# ---------------------------------------------------------------------------
# Stage 3 -- SEC EDGAR knowledge-time guard
# ---------------------------------------------------------------------------


def _donor_sec_edgar_status() -> dict[str, Any]:
    """Probe the harvested ``tradingagents/sec_edgar.py`` without importing it.

    The donor module imports ``tradingagents.dataflows.config`` at module scope
    (vendor plumbing that Ring 2 does not ship), so a plain ``import`` would
    fail.  We therefore record the bridge state and fall back to the native
    acceptance-timestamp implementation below, which reproduces the donor's
    ``fact["filed"] > as_of_date`` rule with nanosecond precision.
    """
    candidate = Path(__file__).resolve().parent / "tradingagents" / "sec_edgar.py"
    if not candidate.exists():
        return {"available": False, "reason": "sec_edgar.py not harvested", "path": ""}
    try:
        import importlib

        importlib.import_module("ring2_brain.truth.tradingagents.sec_edgar")
        return {"available": True, "reason": "", "path": str(candidate)}
    except Exception as exc:  # pragma: no cover - depends on donor vendoring
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "path": str(candidate),
        }


@dataclass(slots=True, frozen=True)
class FilingRecord:
    """One SEC filing indexed by *acceptance* time, never by period end."""

    ticker: str
    form: str
    period_end: str
    acceptance_date: str
    facts: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", str(self.ticker).strip().upper())
        object.__setattr__(self, "form", str(self.form).strip().upper())
        object.__setattr__(self, "facts", {str(k): float(v) for k, v in dict(self.facts).items()})

    @property
    def period_end_ns(self) -> int:
        return iso_to_ns(self.period_end)

    @property
    def knowledge_ts_ns(self) -> int:
        return iso_to_ns(self.acceptance_date)

    def visible_as_of(self, as_of_ns: int) -> bool:
        return self.knowledge_ts_ns <= int(as_of_ns)


class SecEdgarPITGuard:
    """Anti-lookahead guard for fundamental statements (``sec_006`` bullet 2).

    A Q4 10-K covering a December 31 fiscal year end is routinely accepted by
    the SEC in March.  Querying "as of" January must not reveal it.  This guard
    raises :class:`TemporalIntegrityError` whenever a caller asks for a period
    whose acceptance timestamp is still in the future, rather than silently
    returning an older (or empty) statement -- a silent empty result is exactly
    how lookahead bugs hide.
    """

    def __init__(self, *, ticker_aliases: Mapping[str, str] | None = None) -> None:
        self._filings: dict[str, list[FilingRecord]] = defaultdict(list)
        self._aliases = {
            str(k).upper(): str(v).upper() for k, v in dict(ticker_aliases or {}).items()
        }
        self.donor = _donor_sec_edgar_status()

    # -- registration -----------------------------------------------------
    def register_filing(self, filing: FilingRecord) -> FilingRecord:
        ticker = self._aliases.get(filing.ticker, filing.ticker)
        if ticker != filing.ticker:
            filing = FilingRecord(
                ticker=ticker,
                form=filing.form,
                period_end=filing.period_end,
                acceptance_date=filing.acceptance_date,
                facts=filing.facts,
            )
        self._filings[filing.ticker].append(filing)
        self._filings[filing.ticker].sort(key=lambda f: (f.knowledge_ts_ns, f.period_end_ns))
        return filing

    def filings(self, ticker: str) -> tuple[FilingRecord, ...]:
        return tuple(self._filings.get(self._aliases.get(str(ticker).upper(), str(ticker).upper()), ()))

    # -- core guard -------------------------------------------------------
    @staticmethod
    def assert_no_lookahead(
        knowledge_ts_ns: int,
        as_of_ns: int,
        *,
        label: str = "fact",
        valid_time_ns: int | None = None,
    ) -> None:
        """``knowledge_ts_ns <= as_of_ns`` or :class:`TemporalIntegrityError`."""
        if int(knowledge_ts_ns) > int(as_of_ns):
            raise TemporalIntegrityError(
                f"TEMPORAL_LOOKAHEAD: {label} knowledge_ts_ns "
                f"({ns_to_iso(int(knowledge_ts_ns))}) > as_of_ns "
                f"({ns_to_iso(int(as_of_ns))})",
                knowledge_ts_ns=int(knowledge_ts_ns),
                valid_time_ns=valid_time_ns,
                as_of_ns=int(as_of_ns),
            )

    def visible_filings(self, ticker: str, as_of_ns: int) -> tuple[FilingRecord, ...]:
        return tuple(f for f in self.filings(ticker) if f.visible_as_of(as_of_ns))

    def knowledge_time(self, ticker: str, period_end: str) -> int:
        period_ns = iso_to_ns(period_end)
        matches = [f for f in self.filings(ticker) if f.period_end_ns == period_ns]
        if not matches:
            raise KeyError(f"no filing for {ticker} period ending {period_end}")
        return max(f.knowledge_ts_ns for f in matches)

    def statement_as_of(
        self,
        ticker: str,
        as_of_ns: int,
        *,
        period_end: str | None = None,
    ) -> dict[str, float]:
        """Facts for ``period_end`` that were actually public at ``as_of_ns``.

        Raises
        ------
        TemporalIntegrityError
            When the requested period exists but its acceptance timestamp is
            later than ``as_of_ns`` (the January-before-the-March-10-K case).
        KeyError
            When no filing for that period exists at all.
        """
        period_ns = iso_to_ns(period_end) if period_end is not None else None
        matches = [
            f
            for f in self.filings(ticker)
            if period_ns is None or f.period_end_ns == period_ns
        ]
        if period_ns is not None and not matches:
            raise KeyError(f"no filing for {ticker} period ending {period_end}")

        if period_ns is not None:
            visible = [f for f in matches if f.visible_as_of(as_of_ns)]
            if not visible:
                # Nothing about this period is public yet: the earliest
                # acceptance timestamp is necessarily in the future, which is
                # exactly the January-before-the-March-10-K case.
                known = min(f.knowledge_ts_ns for f in matches)
                self.assert_no_lookahead(
                    known,
                    as_of_ns,
                    label=f"{ticker} {matches[0].form} period_end={period_end}",
                    valid_time_ns=period_ns,
                )

        merged: dict[str, float] = {}
        for filing in sorted(matches, key=lambda f: f.knowledge_ts_ns):
            # A later amendment (10-K/A) stays invisible until its own
            # acceptance timestamp; it never invalidates the original filing.
            if filing.visible_as_of(as_of_ns):
                merged.update(filing.facts)
        return merged

    def assert_packet_pit(
        self, knowledge_ts_ns: int, valid_time_ns: int, as_of_ns: int
    ) -> None:
        self.assert_no_lookahead(
            knowledge_ts_ns, as_of_ns, label="packet", valid_time_ns=valid_time_ns
        )
        if int(valid_time_ns) > int(as_of_ns):
            raise TemporalIntegrityError(
                "TEMPORAL_LOOKAHEAD: valid_time_ns references a future bar",
                knowledge_ts_ns=int(knowledge_ts_ns),
                valid_time_ns=int(valid_time_ns),
                as_of_ns=int(as_of_ns),
            )


# ---------------------------------------------------------------------------
# Stage 1 -- harvested multi-vendor router bridge
# ---------------------------------------------------------------------------


class VendorRouterBridge:
    """Thin bridge over the harvested ``tradingagents/router.py``.

    The donor router selects a vendor per data category and fails over on
    ``VendorUnavailableError``.  Ring 2 only needs the *selection* contract, so
    the module is imported lazily and its absence is reported rather than
    crashing ingestion.
    """

    def __init__(self) -> None:
        self._router: Any | None = None
        self._error: str = ""
        try:
            import importlib

            self._router = importlib.import_module(
                "ring2_brain.truth.tradingagents.router"
            )
        except Exception as exc:  # pragma: no cover - donor vendoring
            self._error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._router is not None

    @property
    def reason(self) -> str:
        return self._error

    def route(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._router is None:
            raise RuntimeError(
                "harvested vendor router unavailable: " + (self._error or "unknown")
            )
        return self._router.route_to_vendor(method, *args, **kwargs)

    def categories(self) -> dict[str, str]:
        if self._router is None:
            return {}
        try:
            methods = (
                "get_stock_data",
                "get_indicators",
                "get_fundamentals",
                "get_balance_sheet",
                "get_cashflow",
                "get_income_statement",
                "get_news",
                "get_global_news",
                "get_insider_transactions",
            )
            return {m: self._router.get_category_for_method(m) for m in methods}
        except Exception:  # pragma: no cover - donor schema drift
            return {}


# ---------------------------------------------------------------------------
# Stage 1 -> Stage 2 -- dual-vendor loader router
# ---------------------------------------------------------------------------


class LoaderBinding(Protocol):
    """Minimal loader contract consumed by :class:`DualVendorLoaderRouter`.

    The harvested loaders all expose ``DataLoader.fetch(code, start, end,
    interval)`` returning an OHLCV ``pandas.DataFrame``.  A binding may also be
    a plain callable or an object exposing the same ``fetch`` method, which is
    what the deterministic offline tests use.
    """

    def fetch(
        self,
        code: str,
        start_date: str,
        end_date: str,
        interval: str = "1d",
    ) -> Any: ...


@dataclass(slots=True, frozen=True)
class VendorTick:
    """One vendor's observation of a symbol, with explicit time semantics."""

    vendor: str
    symbol: str
    price: float
    valid_time_ns: int
    knowledge_ts_ns: int
    received_ns: int
    lag_ms: float = 0.0

    def __post_init__(self) -> None:
        price = float(self.price)
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError(f"{self.vendor}: price must be finite and positive")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "valid_time_ns", int(self.valid_time_ns))
        object.__setattr__(self, "knowledge_ts_ns", int(self.knowledge_ts_ns))
        object.__setattr__(self, "received_ns", int(self.received_ns))
        lag = float(self.lag_ms)
        if not math.isfinite(lag) or lag < 0.0:
            lag = 0.0
        object.__setattr__(self, "lag_ms", lag)


@dataclass(slots=True)
class CallableLoader:
    """Deterministic offline loader used by tests and replay harnesses.

    ``fetch_fn`` receives ``(symbol, start_date, end_date, interval)`` and may
    return a price, a mapping, a :class:`VendorTick`, or a DataFrame-like
    object.  It never performs I/O unless the caller's function does.
    """

    name: str
    fetch_fn: Callable[..., Any]

    def fetch(
        self,
        code: str,
        start_date: str,
        end_date: str,
        interval: str = "1d",
    ) -> Any:
        try:
            return self.fetch_fn(code, start_date, end_date, interval)
        except TypeError:
            try:
                return self.fetch_fn(code, start_date, end_date)
            except TypeError:
                return self.fetch_fn(code)


class DualVendorLoaderRouter:
    """Stage 1: fetch the same bar from two independent vendor loaders.

    The router is deliberately *dumb* about market data quality: it only
    produces the :class:`~core.schemas.DualVendorBar` contract and hands it to
    :class:`SelfHealingDataGuard`, which owns every Stage-2 gate.  Keeping the
    two responsibilities separate means a new vendor cannot bypass quarantine
    by "normalizing" its own numbers.

    Parameters
    ----------
    guard:
        The Stage-2/3 :class:`SelfHealingDataGuard` that accepts or rejects the
        bar.  Ring 2 never writes a bar anywhere else.
    primary / secondary:
        Loader instances, loader classes, registry source names (``"yfinance"``,
        ``"stooq"``, ...), or callables.  Strings are resolved through the
        harvested :mod:`...loaders.registry` fallback chain.
    price_field:
        DataFrame column used as the observed price (default ``"close"``).
    """

    def __init__(
        self,
        guard: "SelfHealingDataGuard",
        primary: Any,
        secondary: Any,
        *,
        primary_symbol: str | None = None,
        secondary_symbol: str | None = None,
        lag_gate_ms: float = FEED_LAG_GATE_MS,
        price_field: str = "close",
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if guard is None:
            raise ValueError("DualVendorLoaderRouter requires a SelfHealingDataGuard")
        self.guard = guard
        self.primary_name = self._binding_name(primary, "primary")
        self.secondary_name = self._binding_name(secondary, "secondary")
        self._primary = self._resolve_binding(primary)
        self._secondary = self._resolve_binding(secondary)
        self.primary_symbol = primary_symbol
        self.secondary_symbol = secondary_symbol
        self.lag_gate_ms = float(lag_gate_ms)
        self.price_field = str(price_field)
        self.clock = clock
        self.bars = 0
        self.rejected = 0

    # -- binding resolution ----------------------------------------------
    @staticmethod
    def _binding_name(binding: Any, fallback: str) -> str:
        if isinstance(binding, str):
            return binding
        name = getattr(binding, "name", None) or getattr(binding, "__name__", None)
        return str(name or fallback)

    @staticmethod
    def _resolve_binding(binding: Any) -> Any:
        """Resolve a registry source name into an instantiated loader."""
        if not isinstance(binding, str):
            if isinstance(binding, type):
                return binding()
            return binding
        try:
            from ring2_brain.quant.vibe_trading.loaders import registry as loader_registry

            loader_registry._ensure_registered()
            cls = loader_registry.get_loader_cls_with_fallback(binding)
            return cls()
        except Exception:
            # A registry miss is reported by the guard's quarantine record when
            # the caller actually routes a bar; resolution itself must not
            # crash an ingestion process.
            return None

    # -- payload coercion -------------------------------------------------
    def _call_loader(
        self,
        loader: Any,
        symbol: str,
        start_date: str,
        end_date: str,
        interval: str,
    ) -> Any:
        if loader is None:
            raise RuntimeError("loader binding could not be resolved")
        fetch = getattr(loader, "fetch", None)
        if fetch is None:
            if not callable(loader):
                raise TypeError(
                    f"loader {type(loader).__name__} exposes neither fetch() nor __call__"
                )
            fetch = loader
        try:
            return fetch(symbol, start_date, end_date, interval)
        except TypeError:
            try:
                return fetch(symbol, start_date, end_date)
            except TypeError:
                return fetch(symbol)

    def _coerce_tick(
        self,
        vendor: str,
        symbol: str,
        payload: Any,
        received_ns: int,
        lag_ms: float | None,
    ) -> VendorTick:
        if isinstance(payload, VendorTick):
            return VendorTick(
                vendor=payload.vendor or vendor,
                symbol=payload.symbol or symbol,
                price=payload.price,
                valid_time_ns=payload.valid_time_ns,
                knowledge_ts_ns=payload.knowledge_ts_ns,
                received_ns=payload.received_ns,
                lag_ms=payload.lag_ms if lag_ms is None else lag_ms,
            )

        if isinstance(payload, Mapping):
            price = next(
                (
                    payload[key]
                    for key in ("price", "p1", "close", "last", "mark")
                    if key in payload
                ),
                None,
            )
            if price is None:
                raise ValueError(f"{vendor}: payload has no price field")
            valid_ns = int(payload.get("valid_time_ns") or received_ns)
            knowledge_ns = int(payload.get("knowledge_ts_ns") or received_ns)
            observed_ns = int(payload.get("received_ns") or received_ns)
            payload_lag = payload.get("lag_ms", lag_ms)
            effective_lag = (
                max(0.0, (observed_ns - knowledge_ns) / 1e6)
                if payload_lag is None
                else float(payload_lag)
            )
            return VendorTick(
                vendor=str(payload.get("vendor") or vendor),
                symbol=str(payload.get("symbol") or symbol),
                price=float(price),
                valid_time_ns=valid_ns,
                knowledge_ts_ns=knowledge_ns,
                received_ns=observed_ns,
                lag_ms=effective_lag,
            )

        if isinstance(payload, (tuple, list)):
            if not payload:
                raise ValueError(f"{vendor}: empty tuple payload")
            price = float(payload[0])
            valid_ns = int(payload[1]) if len(payload) > 1 else received_ns
            knowledge_ns = int(payload[2]) if len(payload) > 2 else received_ns
            return VendorTick(
                vendor=vendor,
                symbol=symbol,
                price=price,
                valid_time_ns=valid_ns,
                knowledge_ts_ns=knowledge_ns,
                received_ns=received_ns,
                lag_ms=max(0.0, (received_ns - knowledge_ns) / 1e6)
                if lag_ms is None
                else float(lag_ms),
            )

        if isinstance(payload, (int, float)) and not isinstance(payload, bool):
            return VendorTick(
                vendor=vendor,
                symbol=symbol,
                price=float(payload),
                valid_time_ns=received_ns,
                knowledge_ts_ns=received_ns,
                received_ns=received_ns,
                lag_ms=0.0 if lag_ms is None else float(lag_ms),
            )

        # DataFrame-like: last row's configured price column.
        if hasattr(payload, "empty") and hasattr(payload, "index"):
            if bool(payload.empty):
                raise ValueError(f"{vendor}: empty OHLCV frame for {symbol}")
            row = payload.iloc[-1]
            if self.price_field in getattr(payload, "columns", ()):
                price = row[self.price_field]
            else:
                price = row.iloc[-1] if hasattr(row, "iloc") else row[-1]
            index = payload.index[-1]
            valid_ns = int(getattr(index, "value", received_ns))
            return VendorTick(
                vendor=vendor,
                symbol=symbol,
                price=float(price),
                valid_time_ns=valid_ns,
                knowledge_ts_ns=received_ns,
                received_ns=received_ns,
                lag_ms=0.0 if lag_ms is None else float(lag_ms),
            )

        raise TypeError(f"{vendor}: unsupported loader payload {type(payload).__name__}")

    # -- public API -------------------------------------------------------
    def quote(
        self,
        symbol: str,
        *,
        start_date: str = "",
        end_date: str = "",
        interval: str = "1d",
        as_of_ns: int | None = None,
        primary_symbol: str | None = None,
        secondary_symbol: str | None = None,
        primary_lag_ms: float | None = None,
        secondary_lag_ms: float | None = None,
    ) -> DualVendorBar:
        """Fetch both vendors and return the raw dual-vendor contract."""
        token = str(symbol).strip().upper()
        if not token:
            raise ValueError("symbol must be non-empty")
        received = int(as_of_ns) if as_of_ns is not None else int(self.clock())
        p_symbol = str(primary_symbol or self.primary_symbol or token)
        s_symbol = str(secondary_symbol or self.secondary_symbol or token)

        p_payload = self._call_loader(
            self._primary, p_symbol, start_date, end_date, interval
        )
        s_payload = self._call_loader(
            self._secondary, s_symbol, start_date, end_date, interval
        )
        p_tick = self._coerce_tick(
            self.primary_name, token, p_payload, received, primary_lag_ms
        )
        s_tick = self._coerce_tick(
            self.secondary_name, token, s_payload, received, secondary_lag_ms
        )

        self.bars += 1
        return DualVendorBar(
            symbol=token,
            p1=float(p_tick.price),
            p2=float(s_tick.price),
            feed_lag_ms=float(max(p_tick.lag_ms, s_tick.lag_ms)),
            valid_time_ns=int(max(p_tick.valid_time_ns, s_tick.valid_time_ns)),
            knowledge_ts_ns=int(
                max(p_tick.knowledge_ts_ns, s_tick.knowledge_ts_ns)
            ),
        )

    def route(
        self,
        symbol: str,
        *,
        start_date: str = "",
        end_date: str = "",
        interval: str = "1d",
        as_of_ns: int | None = None,
        primary_symbol: str | None = None,
        secondary_symbol: str | None = None,
        primary_lag_ms: float | None = None,
        secondary_lag_ms: float | None = None,
        currency: str | None = None,
        raise_on_lookahead: bool = True,
    ) -> NormalizedBar | None:
        """Stage 1 -> Stage 2: feed the dual-vendor bar into the guard."""
        bar = self.quote(
            symbol,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
            as_of_ns=as_of_ns,
            primary_symbol=primary_symbol,
            secondary_symbol=secondary_symbol,
            primary_lag_ms=primary_lag_ms,
            secondary_lag_ms=secondary_lag_ms,
        )
        as_of = int(as_of_ns) if as_of_ns is not None else int(bar.knowledge_ts_ns)
        normalized = self.guard.ingest_bar(
            bar,
            as_of_ns=as_of,
            currency=currency,
            raise_on_lookahead=raise_on_lookahead,
        )
        if normalized is None:
            self.rejected += 1
        return normalized

    def route_many(
        self,
        symbols: Iterable[str],
        *,
        start_date: str = "",
        end_date: str = "",
        interval: str = "1d",
        as_of_ns: int | None = None,
        currency: str | None = None,
    ) -> list[NormalizedBar]:
        out: list[NormalizedBar] = []
        for symbol in symbols:
            normalized = self.route(
                symbol,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                as_of_ns=as_of_ns,
                currency=currency,
                raise_on_lookahead=False,
            )
            if normalized is not None:
                out.append(normalized)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "primary": self.primary_name,
            "secondary": self.secondary_name,
            "primary_resolved": self._primary is not None,
            "secondary_resolved": self._secondary is not None,
            "lag_gate_ms": self.lag_gate_ms,
            "bars": self.bars,
            "rejected": self.rejected,
            "guard_accepted": int(self.guard.stats.accepted),
            "guard_rejected": int(self.guard.stats.rejected),
        }


# ---------------------------------------------------------------------------
# Stage 2/3 -- normalized bar and guard
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GuardEvent:
    """Observability record for one accepted/rejected bar."""

    symbol: str
    accepted: bool
    reason: QuarantineReason | None
    detail: str
    valid_time_ns: int
    latency_ms: float


@dataclass(slots=True, frozen=True)
class NormalizedBar:
    """A Stage-3 normalized, PIT-clean, dual-vendor bar."""

    symbol: str
    p1: float
    p2: float
    raw_price: float
    adjusted_price: float
    adjustment_factor: float
    dividend: float
    currency: str
    fx_rate: float
    report_currency: str
    valid_time_ns: int
    knowledge_ts_ns: int
    as_of_ns: int
    sha256_digest: str

    def to_metrics(self) -> dict[str, float]:
        return {
            "price": float(self.adjusted_price),
            "p1": float(self.p1),
            "p2": float(self.p2),
            "adjustment_factor": float(self.adjustment_factor),
            "dividend": float(self.dividend),
            "fx_rate": float(self.fx_rate),
        }


@dataclass(slots=True)
class GuardStats:
    accepted: int = 0
    rejected: int = 0
    lag_rejected: int = 0
    divergence_rejected: int = 0
    split_rejected: int = 0
    lookahead_rejected: int = 0
    events: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class SelfHealingDataGuard:
    """Stages 1-3 of the ``sec_006`` ingestion pipeline."""

    def __init__(
        self,
        quarantine: QuarantineStore | None = None,
        *,
        security_master: SecurityMaster | None = None,
        corporate_actions: CorporateActionBook | None = None,
        fx: FxBook | None = None,
        sec_guard: SecEdgarPITGuard | None = None,
        report_currency: str = "USD",
        event_log_size: int = 4_096,
    ) -> None:
        self.quarantine = quarantine if quarantine is not None else QuarantineStore()
        self.security_master = security_master or SecurityMaster()
        self.corporate_actions = corporate_actions or CorporateActionBook()
        self.fx = fx or FxBook()
        self.sec_guard = sec_guard or SecEdgarPITGuard()
        self.router = VendorRouterBridge()
        self.report_currency = str(report_currency).upper()
        self.stats = GuardStats()
        self._events: deque[GuardEvent] = deque(maxlen=int(event_log_size))

    # -- helpers ----------------------------------------------------------
    @property
    def events(self) -> tuple[GuardEvent, ...]:
        return tuple(self._events)

    def _reject(
        self,
        bar: DualVendorBar,
        reason: QuarantineReason,
        detail: str,
        as_of_ns: int,
        elapsed_ms: float,
    ) -> None:
        self.quarantine.record(reason, bar, as_of_ns=as_of_ns, detail=detail)
        self.stats.rejected += 1
        self.stats.events += 1
        if reason is QuarantineReason.FEED_LAG_EXCEEDED:
            self.stats.lag_rejected += 1
        elif reason is QuarantineReason.CROSS_VENDOR_DIVERGENCE:
            self.stats.divergence_rejected += 1
        elif reason is QuarantineReason.UNANNOUNCED_SPLIT_GAP:
            self.stats.split_rejected += 1
        elif reason is QuarantineReason.TEMPORAL_LOOKAHEAD:
            self.stats.lookahead_rejected += 1
        self._events.append(
            GuardEvent(
                symbol=str(bar.symbol),
                accepted=False,
                reason=reason,
                detail=detail,
                valid_time_ns=int(bar.valid_time_ns),
                latency_ms=elapsed_ms,
            )
        )

    # -- Stage 2 + 3 -------------------------------------------------------
    def ingest_bar(
        self,
        bar: DualVendorBar,
        *,
        as_of_ns: int | None = None,
        currency: str | None = None,
        raise_on_lookahead: bool = True,
    ) -> NormalizedBar | None:
        """Run every gate, normalize the survivor, or quarantine the bar.

        Returns ``None`` when the bar is rejected.  A PIT violation additionally
        raises :class:`TemporalIntegrityError` (after being recorded), because a
        lookahead is a bug in the caller, not a flaky feed.
        """
        started = time.perf_counter()
        as_of = int(as_of_ns if as_of_ns is not None else max(bar.valid_time_ns, bar.knowledge_ts_ns))

        # Gate 1 -- feed lag.
        if float(bar.feed_lag_ms) > FEED_LAG_GATE_MS:
            self._reject(
                bar,
                QuarantineReason.FEED_LAG_EXCEEDED,
                f"feed_lag_ms={float(bar.feed_lag_ms):.3f} > {FEED_LAG_GATE_MS:.0f}",
                as_of,
                (time.perf_counter() - started) * 1e3,
            )
            return None

        # Gate 2 -- unannounced split (specific diagnosis before generic divergence).
        if float(bar.split_gap_pct) >= SPLIT_GUARD_PCT:
            self._reject(
                bar,
                QuarantineReason.UNANNOUNCED_SPLIT_GAP,
                f"single-feed gap {float(bar.split_gap_pct):.3f}% >= {SPLIT_GUARD_PCT:.0f}%",
                as_of,
                (time.perf_counter() - started) * 1e3,
            )
            return None

        # Gate 3 -- cross-vendor divergence.
        if float(bar.vendor_divergence_pct) > VENDOR_DIVERGENCE_GATE_PCT:
            self._reject(
                bar,
                QuarantineReason.CROSS_VENDOR_DIVERGENCE,
                f"divergence {float(bar.vendor_divergence_pct):.4f}% > "
                f"{VENDOR_DIVERGENCE_GATE_PCT:.2f}%",
                as_of,
                (time.perf_counter() - started) * 1e3,
            )
            return None

        # Gate 4 -- point-in-time integrity.
        if int(bar.knowledge_ts_ns) > as_of or int(bar.valid_time_ns) > as_of:
            self._reject(
                bar,
                QuarantineReason.TEMPORAL_LOOKAHEAD,
                f"knowledge_ts_ns={bar.knowledge_ts_ns} valid_time_ns="
                f"{bar.valid_time_ns} as_of_ns={as_of}",
                as_of,
                (time.perf_counter() - started) * 1e3,
            )
            if raise_on_lookahead:
                raise TemporalIntegrityError(
                    "TEMPORAL_LOOKAHEAD: bar would leak future knowledge",
                    knowledge_ts_ns=int(bar.knowledge_ts_ns),
                    valid_time_ns=int(bar.valid_time_ns),
                    as_of_ns=as_of,
                )
            return None

        symbol = self.security_master.resolve(bar.symbol)
        raw_price = float(bar.price)
        factor = self.corporate_actions.adjustment_factor(
            symbol, int(bar.valid_time_ns), as_of
        )
        dividend = self.corporate_actions.dividend_since(
            symbol, int(bar.valid_time_ns), as_of
        )
        ccy = str(currency or self.report_currency).upper()
        fx_rate = self.fx.rate(ccy, self.report_currency, as_of)

        adjusted = (raw_price * factor - dividend) * fx_rate
        digest = canonical_pairs_digest(
            [
                ("symbol", symbol),
                ("p1", float(bar.p1)),
                ("p2", float(bar.p2)),
                ("valid_time_ns", int(bar.valid_time_ns)),
                ("knowledge_ts_ns", int(bar.knowledge_ts_ns)),
                ("as_of_ns", as_of),
                ("factor", factor),
                ("dividend", dividend),
                ("fx_rate", fx_rate),
            ]
        )
        normalized = NormalizedBar(
            symbol=symbol,
            p1=float(bar.p1),
            p2=float(bar.p2),
            raw_price=raw_price,
            adjusted_price=float(adjusted),
            adjustment_factor=float(factor),
            dividend=float(dividend),
            currency=ccy,
            fx_rate=float(fx_rate),
            report_currency=self.report_currency,
            valid_time_ns=int(bar.valid_time_ns),
            knowledge_ts_ns=int(bar.knowledge_ts_ns),
            as_of_ns=as_of,
            sha256_digest=digest,
        )
        self.stats.accepted += 1
        self.stats.events += 1
        self._events.append(
            GuardEvent(
                symbol=symbol,
                accepted=True,
                reason=None,
                detail=f"price={normalized.adjusted_price:.6f} {self.report_currency}",
                valid_time_ns=normalized.valid_time_ns,
                latency_ms=(time.perf_counter() - started) * 1e3,
            )
        )
        return normalized

    def ingest_batch(
        self,
        bars: Iterable[DualVendorBar],
        *,
        as_of_ns: int | None = None,
        currency: str | None = None,
    ) -> list[NormalizedBar]:
        out: list[NormalizedBar] = []
        for bar in bars:
            normalized = self.ingest_bar(
                bar, as_of_ns=as_of_ns, currency=currency, raise_on_lookahead=False
            )
            if normalized is not None:
                out.append(normalized)
        return out

    # -- SEC convenience ---------------------------------------------------
    def statement_as_of(
        self, ticker: str, as_of_ns: int, *, period_end: str | None = None
    ) -> dict[str, float]:
        return self.sec_guard.statement_as_of(ticker, as_of_ns, period_end=period_end)

    def assert_pit(self, knowledge_ts_ns: int, valid_time_ns: int, as_of_ns: int) -> None:
        self.sec_guard.assert_packet_pit(knowledge_ts_ns, valid_time_ns, as_of_ns)


# ---------------------------------------------------------------------------
# Stage 5 -- orderflow normalizer (fast path)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FlushReport:
    symbol_count: int
    frame_count: int
    dispatched: int
    p50_ms: float
    p99_ms: float
    max_ms: float


class OrderflowNormalizer:
    """Aggregate raw trades/L2 updates into 5-second executed-flow frames.

    Only *executed* prints are aggregated: resting depth is never used as proof
    of liquidity (Law 1 / ``sec_018``).  The hot path appends each finished
    frame into :class:`core.shm_ipc.OfiRingBuffer` with no I/O.
    """

    def __init__(
        self,
        ofi_ring: Any,
        *,
        window_ns: int = FIVE_SECONDS_NS,
        vol_alpha: float = 0.05,
        max_trades: int = 8_192,
        max_latency_samples: int = 4_096,
    ) -> None:
        self.ring = ofi_ring
        self.window_ns = int(window_ns)
        self.vol_alpha = float(vol_alpha)
        self._max_trades = int(max_trades)
        self._trades: dict[str, deque[tuple[int, float, float, float]]] = defaultdict(
            lambda: deque(maxlen=self._max_trades)
        )
        self._book: dict[str, tuple[float, float, float, float]] = {}
        self._cancels: dict[str, deque[tuple[int, float, float]]] = defaultdict(
            lambda: deque(maxlen=4_096)
        )
        self._fills: dict[str, deque[tuple[int, float, float]]] = defaultdict(
            lambda: deque(maxlen=4_096)
        )
        self._baseline: dict[str, float] = {}
        self._latencies_ms: deque[float] = deque(maxlen=int(max_latency_samples))
        self._last_price: dict[str, float] = {}

    # -- ingest -----------------------------------------------------------
    def ingest_trade(
        self,
        symbol: str,
        price: float,
        qty: float,
        side: str,
        ts_ns: int,
    ) -> None:
        token = "BUY" if str(side).strip().upper() in ("BUY", "B", "LONG") else "SELL"
        key = str(symbol).strip().upper()
        self._trades[key].append((int(ts_ns), float(price), float(qty), 1.0 if token == "BUY" else -1.0))
        self._fills[key].append((int(ts_ns), float(qty), 1.0))
        self._last_price[key] = float(price)

    def ingest_book(
        self,
        symbol: str,
        best_bid: float,
        best_ask: float,
        *,
        bid_qty: float = 0.0,
        ask_qty: float = 0.0,
        bid_cancels: float = 0.0,
        ask_cancels: float = 0.0,
        ts_ns: int | None = None,
    ) -> None:
        key = str(symbol).strip().upper()
        self._book[key] = (float(best_bid), float(best_ask), float(bid_qty), float(ask_qty))
        if bid_cancels or ask_cancels:
            self._cancels[key].append(
                (int(ts_ns if ts_ns is not None else time.time_ns()), float(bid_cancels), float(ask_cancels))
            )

    # -- frame construction ----------------------------------------------
    def build_frame(self, symbol: str, now_ns: int) -> ExecutedFlowTick | None:
        key = str(symbol).strip().upper()
        trades = self._trades.get(key)
        if not trades:
            return None
        cutoff = int(now_ns) - self.window_ns
        window = [t for t in trades if t[0] >= cutoff]
        if not window:
            return None

        buy = sum(t[2] for t in window if t[3] > 0)
        sell = sum(t[2] for t in window if t[3] < 0)
        prices = [t[1] for t in window]
        last_price = prices[-1]

        returns: list[float] = []
        for previous, current in zip(prices, prices[1:]):
            if previous > 0.0 and current > 0.0:
                returns.append(math.log(current / previous))
        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            realized_vol_bps = math.sqrt(max(variance, 0.0)) * 10_000.0
        elif len(returns) == 1:
            realized_vol_bps = abs(returns[0]) * 10_000.0
        else:
            realized_vol_bps = 0.0

        baseline = self._baseline.get(key)
        if baseline is None or baseline <= 0.0:
            baseline = max(realized_vol_bps, 1.0)
        else:
            baseline = (1.0 - self.vol_alpha) * baseline + self.vol_alpha * max(realized_vol_bps, 1e-6)
        self._baseline[key] = baseline

        bid, ask, _bq, _aq = self._book.get(key, (0.0, 0.0, 0.0, 0.0))
        mid = 0.5 * (bid + ask) if bid > 0.0 and ask > 0.0 else last_price
        spread_bps = (
            (ask - bid) / mid * 10_000.0 if mid > 0.0 and ask >= bid > 0.0 else 0.0
        )

        fills = [f for f in self._fills.get(key, ()) if f[0] >= cutoff]
        cancels = [c for c in self._cancels.get(key, ()) if c[0] >= cutoff]
        filled = max(1.0, sum(f[1] for f in fills))
        bid_c2f = sum(c[1] for c in cancels) / filled
        ask_c2f = sum(c[2] for c in cancels) / filled

        return ExecutedFlowTick(
            symbol=key,
            valid_time_ns=int(now_ns),
            knowledge_ts_ns=int(now_ns),
            last_price=float(last_price),
            spread_bps=float(spread_bps),
            realized_vol_5s=float(realized_vol_bps),
            normal_vol_5s=float(baseline),
            taker_buy_volume_5s=float(buy),
            taker_sell_volume_5s=float(sell),
            bid_cancel_to_fill_ratio=float(bid_c2f),
            ask_cancel_to_fill_ratio=float(ask_c2f),
        )

    # -- dispatch ---------------------------------------------------------
    def dispatch(self, tick: ExecutedFlowTick) -> float:
        """Append one frame to the Ring 1 ring buffer; return latency in ms."""
        started = time.perf_counter()
        self.ring.append(tick)
        elapsed_ms = (time.perf_counter() - started) * 1e3
        self._latencies_ms.append(elapsed_ms)
        return elapsed_ms

    def flush(self, now_ns: int | None = None) -> FlushReport:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        symbols = list(self._trades.keys())
        frames = 0
        dispatched = 0
        for symbol in symbols:
            frame = self.build_frame(symbol, stamp)
            if frame is None:
                continue
            frames += 1
            self.dispatch(frame)
            dispatched += 1
        samples = list(self._latencies_ms)
        return FlushReport(
            symbol_count=len(symbols),
            frame_count=frames,
            dispatched=dispatched,
            p50_ms=percentile(samples, 0.50),
            p99_ms=percentile(samples, 0.99),
            max_ms=max(samples) if samples else 0.0,
        )

    @property
    def latencies_ms(self) -> tuple[float, ...]:
        return tuple(self._latencies_ms)

    def reset_latencies(self) -> None:
        self._latencies_ms.clear()
