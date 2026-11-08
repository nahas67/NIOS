"""Progressive ``SKILL.md`` loader for System 2 cognition (Step 8).

The ring-2 brain pays for a skill in two stages:

1. **Startup** -- only the YAML frontmatter (``name``, ``description``, ``silo``)
   is read.  The markdown body never enters the prompt, so a library of domain
   skills costs a few hundred tokens, not tens of thousands.
2. **On demand** -- when System 2 analyzes a symbol in a matching silo,
   :meth:`SkillRegistry.load_for_silo` reads the body of only the matching
   skills and caches it for the process lifetime.

The four seeded domain skills encode the cost floors and failure modes that
matter to the four live silos: India F&O STT + dealer gamma, SEC 10-K PIT
forensics, crypto funding cascades, and FX macro blackout windows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CORE_SKILLS",
    "SkillMetadata",
    "SkillRegistry",
    "parse_frontmatter",
]

#: The four core domain skills seeded with the runtime.
CORE_SKILLS: tuple[str, ...] = (
    "india_fno_stt_gamma",
    "sec_10k_pit_forensics",
    "crypto_funding_cascade",
    "fx_macro_blackout",
)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [token.strip().strip("'\"") for token in inner.split(",")]
    if (text.startswith("'") and text.endswith("'")) or (
        text.startswith('"') and text.endswith('"')
    ):
        return text[1:-1]
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``SKILL.md`` text into ``(frontmatter, body)``.

    Parsing is intentionally a tiny YAML subset (``key: value`` plus inline
    lists) so startup never imports PyYAML in the hot process.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    header = match.group(1)
    body = text[match.end():]
    data: dict[str, Any] = {}
    for line in header.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        data[key.strip()] = _parse_scalar(value)
    return data, body


@dataclass
class SkillMetadata:
    """Frontmatter-only view of one skill (no body loaded)."""

    name: str
    description: str
    silo: str
    path: str
    tags: tuple[str, ...] = ()
    enabled: bool = True
    body_loaded: bool = False
    body_chars: int = 0
    body_tokens_estimate: int = 0
    frontmatter_tokens_estimate: int = 0
    load_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "silo": self.silo,
            "tags": list(self.tags),
            "path": self.path,
            "enabled": self.enabled,
            "body_loaded": self.body_loaded,
            "body_chars": self.body_chars,
            "body_tokens_estimate": self.body_tokens_estimate,
            "frontmatter_tokens_estimate": self.frontmatter_tokens_estimate,
            "load_count": self.load_count,
        }


@dataclass
class _SkillEntry:
    metadata: SkillMetadata
    path: Path
    body: str | None = None


class SkillRegistry:
    """Progressive markdown skill loader rooted at ``ring2_brain/skills``."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        auto_discover: bool = True,
        seed_if_empty: bool = True,
    ) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, _SkillEntry] = {}
        self._disabled: set[str] = set()
        self._load_order: list[str] = []
        if seed_if_empty and not any(self.root.glob("*/SKILL.md")):
            seed_core_skills(self.root)
        if auto_discover:
            self.discover()

    # -- discovery --------------------------------------------------------
    def discover(self) -> int:
        """Read frontmatter only; never load a body into memory."""
        discovered = 0
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                header_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            frontmatter, _ = parse_frontmatter(header_text)
            name = str(frontmatter.get("name") or path.parent.name).strip()
            if not name:
                continue
            description = str(frontmatter.get("description", "")).strip()
            silo = str(frontmatter.get("silo", "")).strip()
            tags = frontmatter.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            header_tokens = max(
                1, int((len(header_text) - len(header_text)) / 4)  # frontmatter only
            )
            # Frontmatter token estimate is computed from the raw header to
            # keep the accounting honest without loading the body.
            match = _FRONTMATTER_RE.match(header_text)
            if match:
                header_tokens = max(1, len(match.group(0)) // 4)
            metadata = SkillMetadata(
                name=name,
                description=description,
                silo=silo,
                path=str(path),
                tags=tuple(str(tag) for tag in tags),
                enabled=name not in self._disabled,
                frontmatter_tokens_estimate=header_tokens,
            )
            self._skills[name] = _SkillEntry(metadata=metadata, path=path)
            discovered += 1
        return discovered

    def register(self, path: str | Path) -> SkillMetadata:
        """Register a single ``SKILL.md`` at runtime (frontmatter only)."""
        skill_path = Path(path)
        if skill_path.is_dir():
            skill_path = skill_path / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(text)
        name = str(frontmatter.get("name") or skill_path.parent.name).strip()
        if not name:
            raise ValueError("SKILL.md must declare a name")
        match = _FRONTMATTER_RE.match(text)
        tags = frontmatter.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        metadata = SkillMetadata(
            name=name,
            description=str(frontmatter.get("description", "")).strip(),
            silo=str(frontmatter.get("silo", "")).strip(),
            path=str(skill_path),
            tags=tuple(str(tag) for tag in tags),
            enabled=name not in self._disabled,
            frontmatter_tokens_estimate=max(1, len(match.group(0)) // 4) if match else 1,
        )
        self._skills[name] = _SkillEntry(metadata=metadata, path=skill_path)
        return metadata

    def unregister(self, name: str) -> bool:
        return self._skills.pop(str(name), None) is not None

    # -- progressive loading ---------------------------------------------
    def load(self, name: str, *, force: bool = False) -> str:
        """Load and cache one skill body on demand."""
        token = str(name)
        entry = self._skills.get(token)
        if entry is None:
            raise KeyError(f"unknown skill {token!r}")
        if token in self._disabled and not force:
            raise PermissionError(f"skill {token!r} is disabled")
        if entry.body is not None:
            return entry.body
        text = entry.path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        entry.body = body.strip()
        entry.metadata.body_loaded = True
        entry.metadata.body_chars = len(entry.body)
        entry.metadata.body_tokens_estimate = max(1, len(entry.body) // 4)
        entry.metadata.load_count += 1
        self._load_order.append(token)
        return entry.body

    def load_for_silo(self, silo: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Load only the enabled skills whose frontmatter matches ``silo``."""
        token = str(silo).strip().upper()
        matches: list[dict[str, Any]] = []
        for name, entry in self._skills.items():
            if not entry.metadata.enabled:
                continue
            declared = entry.metadata.silo.upper()
            if declared and declared not in token and token not in declared:
                continue
            body = self.load(name)
            matches.append(
                {
                    "name": name,
                    "silo": entry.metadata.silo,
                    "description": entry.metadata.description,
                    "body": body,
                }
            )
            if limit is not None and len(matches) >= int(limit):
                break
        return matches

    def for_silo(self, silo: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.load_for_silo(silo, limit=limit)

    # -- lifecycle --------------------------------------------------------
    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        token = str(name)
        entry = self._skills.get(token)
        if entry is None:
            raise KeyError(f"unknown skill {token!r}")
        if enabled:
            self._disabled.discard(token)
        else:
            self._disabled.add(token)
        entry.metadata.enabled = bool(enabled)
        return entry.metadata.to_dict()

    def toggle(self, name: str, enabled: bool) -> dict[str, Any]:
        return self.set_enabled(name, enabled)

    def get(self, name: str) -> SkillMetadata | None:
        entry = self._skills.get(str(name))
        return entry.metadata if entry is not None else None

    def list_skills(self) -> list[dict[str, Any]]:
        return [entry.metadata.to_dict() for entry in self._skills.values()]

    def describe(self) -> dict[str, Any]:
        """Frontmatter-only snapshot; safe to call on every cockpit frame."""
        skills = self.list_skills()
        return {
            "count": len(skills),
            "skills": skills,
            "bodies_loaded": sum(1 for item in skills if item["body_loaded"]),
            "loaded_tokens_estimate": sum(
                item["body_tokens_estimate"] for item in skills if item["body_loaded"]
            ),
            "token_overhead": sum(
                item["frontmatter_tokens_estimate"] for item in skills
            ),
            "progressive": True,
            "load_order": list(self._load_order),
            "enabled_count": sum(1 for item in skills if item["enabled"]),
        }

    def snapshot(self) -> dict[str, Any]:
        return self.describe()


# ---------------------------------------------------------------------------
# Seed content
# ---------------------------------------------------------------------------

_SEED_CONTENT: dict[str, str] = {
    "india_fno_stt_gamma": """---
name: india_fno_stt_gamma
description: India F&O edge must clear 5.5 bps STT/fee floor plus dealer-gamma pin risk before any scalp; models expiry-day gamma flips and Dhan/Zerodha margin drift.
silo: INDIA
tags: [india, fno, stt, gamma, expiry]
---

# India F&O: STT, statutory floor and dealer gamma

## Cost floor (never negotiate below it)
- Statutory STT on equity delivery is 0.1% both sides; on intraday F&O the
  effective round-trip tax + exchange fee + stamp duty floor is **5.5 bps** in
  the silo cost table. The conformal gate must subtract it before the Net_LB
  test.
- Brokerage is zero for delivery but not for F&O; the silo registry owns the
  authoritative maker/taker/STT tuple. Never hard-code a lower floor.

## Expiry-day dealer gamma
- When the NIFTY/BANKNIFTY spot sits within 0.35% of a strike with large open
  interest, dealer hedging can pin the tape. A positive `dealer_gamma_flip`
  proximity in the MiroFish shock vector is a warning, not a buy signal.
- A long scalp requires executed taker buy ratio >= 0.65 and cancel-to-fill
  < 3.0 after the pin window. Resting depth at the strike is spoofable.

## Failure modes
- STT is charged on the sell side even when the strategy loses; a +4 bps gross
  scalp is a guaranteed net loss after the 5.5 bps floor.
- Expiry-week margin spikes can freeze the silo; defer to the SiloGuard freeze
  reason rather than retrying the order.
""",
    "sec_10k_pit_forensics": """---
name: sec_10k_pit_forensics
description: SEC 10-K/10-Q point-in-time forensics; statementKnowledgeTime, restatement handling, filing acceptance cutoff, and unadjusted split traps.
silo: US
tags: [sec, edgar, pit, 10-k, fundamentals]
---

# SEC 10-K/10-Q point-in-time forensics

## The hard rule
Every filing fact carries `statementKnowledgeTime` (the EDGAR acceptance
timestamp) and `validTime` (the fiscal period). A fact may enter a thesis only
when `knowledgeTime <= asOfTime`. The `sec_edgar.py` PIT guard enforces this;
do not bypass it with a fiscal-period lookup.

## Restatements and amendments
- 10-K/A and 10-Q/A replace the original for *future* knowledge times only.
  A backtest at 2024-03-01 must use the original filing even if an amendment
  lands in 2025.
- If two filings share a fiscal period, prefer the later acceptance time only
  when it is still <= asOfTime.

## Split and dividend traps
- Yahoo/Stooq close series are split-adjusted; the filing's per-share figures
  are as-reported. Reconcile the split factor before comparing EPS or book
  value. A 4:1 split makes an unadjusted EPS look 4x too high.
- Use the dual-vendor divergence gate (>1.50%) and the split guard (>=45%) to
  quarantine a mismatched series rather than silently repairing it.

## Output discipline
- Every derived multiple (P/E, EV/EBITDA, FCF yield) must carry the SHA-256
  digest of the GroundedFactPacket that supplied its inputs.
""",
    "crypto_funding_cascade": """---
name: crypto_funding_cascade
description: Crypto perpetual funding, basis and liquidation cascades; detects crowded leverage, funding flips, and exchange-specific liquidation clustering before a scalp.
silo: CRYPTO
tags: [crypto, funding, basis, liquidation, perp]
---

# Crypto funding and liquidation cascades

## Funding is a carry cost, not a signal by itself
- A positive perp funding rate means longs pay shorts. A crowded long with
  funding above the 30-day 90th percentile is fragile, not bullish.
- The silo cost floor must include the funding interval that straddles the
  expected holding period. A 3-second scalp can ignore funding; a 4-hour swing
  cannot.

## Cascade signatures
- Watch the three-tick MiroFish cascade: rising `crowd_asymmetry_score` with a
  negative `narrative_exhaustion_divergence` is a crowded long into exhaustion.
- A `cascade_fragility_index >= 0.75` revokes long leases immediately. Do not
  average down into a liquidation cluster.
- Exchange-specific liquidation engines cluster at round numbers and at the
  prior high/low. A wick through the cluster is often the low-liquidity fill,
  not a new trend.

## Execution
- Perp maker rebates differ by venue. The silo registry owns the tuple.
- A resting bid is never proof of liquidity: require executed taker sell flow
  at or through the peg before the shadow twin records a fill.
""",
    "fx_macro_blackout": """---
name: fx_macro_blackout
description: FX macro-event blackout windows; NFP/CPI/FOMC/ECB release timing, spread blowouts, MT5 execution risk, and when to abstain.
silo: FX
tags: [fx, macro, blackout, mt5, spread]
---

# FX macro blackout windows

## Blackout calendar
- Do not initiate a new scalp in the 15 minutes before and 5 minutes after a
  tier-1 release: US NFP, US CPI, FOMC statement/presser, ECB decision, and
  the London 16:00 fix.
- Existing positions are reduced, not added. The ADR-009 reduce-only path is
  the correct tool; the kill switch is reserved for a broken venue.

## Spread and slippage
- MT5 spreads can widen 5-20x around a release. The 12 bps max-spread gate is
  a hard rejection, not a suggestion.
- Require dual-vendor agreement; a single broker quote during a blackout is a
  hypothesis. The feed quarantine drops a vendor on >1.50% divergence.

## Event risk after the print
- The first 60 seconds are liquidity vacuum; the second move is often the
  real repricing. Wait for executed taker flow to normalize before arming a
  lease.
- A macro surprise can invalidate a technical setup entirely; the committee
  must see the release timestamp in the GroundedFactPacket before publishing a
  thesis.
""",
}


def seed_core_skills(root: str | Path | None = None) -> list[Path]:
    """Write the four core ``SKILL.md`` files if they do not exist."""
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    written: list[Path] = []
    for name, content in _SEED_CONTENT.items():
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "SKILL.md"
        if not target.exists():
            target.write_text(content, encoding="utf-8")
            written.append(target)
    return written
