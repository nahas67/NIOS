"""Ontology normalisation used by the MiroFish generator."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "MAX_ONTOLOGY_TYPES",
    "normalize_ontology_attributes",
    "normalize_ontology_source_targets",
]

MAX_ONTOLOGY_TYPES = 12


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def normalize_ontology_attributes(attributes: Any) -> list[dict[str, Any]]:
    """Return a de-duplicated list of ``{name, type?}`` attribute records."""
    if not attributes:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in attributes:
        if isinstance(item, Mapping):
            name = str(item.get("name") or item.get("attribute") or "").strip()
            kind = item.get("type")
        else:
            name, kind = str(item).strip(), None
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        record: dict[str, Any] = {"name": name}
        if kind:
            record["type"] = str(kind)
        out.append(record)
    return out


def normalize_ontology_source_targets(
    edges: Any,
    node_names: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Keep edges whose endpoints exist and de-duplicate them."""
    known = {str(name).strip().casefold() for name in node_names}
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges or ():
        if not isinstance(edge, Mapping):
            continue
        source = str(edge.get("source") or edge.get("from") or "").strip()
        target = str(edge.get("target") or edge.get("to") or "").strip()
        relation = str(edge.get("type") or edge.get("relation") or "related_to").strip()
        if not source or not target:
            continue
        if known and (source.casefold() not in known or target.casefold() not in known):
            continue
        key = (source.casefold(), target.casefold(), relation.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": source, "target": target, "type": relation})
    return out
