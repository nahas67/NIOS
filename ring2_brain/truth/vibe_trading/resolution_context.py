"""Resolution context for the grounding identity gate.

Upstream's ``src.agent.resolution_context`` turns market words in the user's
request into explicit constraints the resolver must respect.  The harvested
grounding package imports it, so this module implements that contract:
:class:`IdentityConstraint` records one dimension/value pair, and
:class:`ResolutionContext` extracts them from the user message and history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["IdentityConstraint", "ResolutionContext", "candidate_market"]

_MARKET_WORDS: dict[str, tuple[str, ...]] = {
    "us": (
        "us",
        "u.s.",
        "usa",
        "united states",
        "american",
        "nasdaq",
        "nyse",
        "s&p",
        "wall street",
    ),
    "india": ("india", "indian", "nse", "bse", "nifty", "sensex", "rupee"),
    "hk": ("hong kong", "hkex", "hang seng", "hsi", "hk"),
    "cn": ("china", "chinese", "a-share", "a share", "shanghai", "shenzhen", "csi"),
    "kr": ("korea", "korean", "krx", "kospi", "kosdaq"),
    "jp": ("japan", "japanese", "tokyo", "nikkei", "tse"),
    "uk": ("uk", "u.k.", "britain", "british", "london", "ftse", "gbp"),
    "eu": ("europe", "european", "eurozone", "euronext", "dax", "cac"),
    "crypto": ("crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth"),
    "fx": ("forex", "fx", "currency pair"),
}

_MARKET_ALIASES: dict[str, str] = {
    "united states": "us",
    "u.s.": "us",
    "usa": "us",
    "american": "us",
    "nasdaq": "us",
    "nyse": "us",
    "indian": "india",
    "nse": "india",
    "bse": "india",
    "hong kong": "hk",
    "hkex": "hk",
    "chinese": "cn",
    "a-share": "cn",
    "a share": "cn",
    "shanghai": "cn",
    "shenzhen": "cn",
    "korean": "kr",
    "krx": "kr",
    "japanese": "jp",
    "tokyo": "jp",
    "british": "uk",
    "london": "uk",
    "european": "eu",
    "bitcoin": "crypto",
    "btc": "crypto",
    "ethereum": "crypto",
    "eth": "crypto",
    "forex": "fx",
}

_EXCHANGE_MARKETS: dict[str, str] = {
    "NASDAQ": "us",
    "NYSE": "us",
    "AMEX": "us",
    "ARCA": "us",
    "BATS": "us",
    "US": "us",
    "NSE": "india",
    "BSE": "india",
    "NSEIN": "india",
    "HKEX": "hk",
    "HK": "hk",
    "SSE": "cn",
    "SZSE": "cn",
    "SH": "cn",
    "SZ": "cn",
    "CN": "cn",
    "KRX": "kr",
    "KOSPI": "kr",
    "KOSDAQ": "kr",
    "KR": "kr",
    "TSE": "jp",
    "JPX": "jp",
    "JP": "jp",
    "LSE": "uk",
    "LON": "uk",
    "UK": "uk",
    "EURONEXT": "eu",
    "XETRA": "eu",
    "EU": "eu",
    "CRYPTO": "crypto",
    "COINBASE": "crypto",
    "BINANCE": "crypto",
    "OKX": "crypto",
    "BYBIT": "crypto",
    "FOREX": "fx",
    "FX": "fx",
}


def candidate_market(candidate: Mapping[str, Any] | Any) -> str | None:
    """Return the normalised market key for a resolver candidate."""
    if candidate is None:
        return None
    if isinstance(candidate, Mapping):
        getter = candidate.get
    else:
        def getter(key: str, default: Any = None) -> Any:
            return getattr(candidate, key, default)

    for key in ("market", "exchange", "venue", "mic", "region"):
        value = getter(key)
        if value:
            text = str(value).strip().upper()
            if text in _EXCHANGE_MARKETS:
                return _EXCHANGE_MARKETS[text]
            lowered = text.casefold()
            for market, words in _MARKET_WORDS.items():
                if any(word == lowered or word in lowered for word in words):
                    return market
    return None


def _market_from_word(word: str) -> str | None:
    text = word.strip().casefold()
    if text in _MARKET_ALIASES:
        return _MARKET_ALIASES[text]
    for market, words in _MARKET_WORDS.items():
        if any(alias == text for alias in words):
            return market
    return None


@dataclass(frozen=True, slots=True)
class IdentityConstraint:
    """One explicit dimension/value constraint from the conversation."""

    dimension: str
    value: str
    explicit: bool = True
    source: str = "user_message"

    def audit_record(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "explicit": self.explicit,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Constraints that narrow resolver candidates."""

    constraints: tuple[IdentityConstraint, ...] = field(default_factory=tuple)

    @classmethod
    def from_messages(
        cls,
        user_message: str,
        history: Sequence[Mapping[str, Any]] | None = None,
        *,
        enabled: bool = True,
    ) -> "ResolutionContext":
        if not enabled:
            return cls()
        text = str(user_message or "")
        found: list[IdentityConstraint] = []
        seen: set[tuple[str, str]] = set()
        for raw in re.findall(r"[A-Za-z][A-Za-z.&\- ]{0,24}", text):
            clause = raw.strip(" .,-")
            if not clause:
                continue
            lowered = clause.casefold()
            market = None
            if lowered in _MARKET_ALIASES:
                market = _MARKET_ALIASES[lowered]
            else:
                for word in _MARKET_ALIASES:
                    if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
                        market = _MARKET_ALIASES[word]
                        break
            if market is not None:
                key = ("market", market)
                if key not in seen:
                    seen.add(key)
                    found.append(
                        IdentityConstraint(dimension="market", value=market, source="user_message")
                    )
        return cls(constraints=tuple(found))

    def constraints_for(self, query: str | None = None) -> list[IdentityConstraint]:
        """Return the constraints relevant to ``query``.

        Market constraints are global per request; they apply to every resolver
        query in the run.  Keeping the method on the context preserves the
        donor's call shape and leaves room for per-query filtering later.
        """
        return list(self.constraints)
