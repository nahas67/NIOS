"""Local-only entity graph reader for the MiroFish contagion core.

Upstream MiroFish stores its ontology graph in a cloud service. NIOS forbids
that dependency, so this module keeps the same data model and filtering
semantics but reads a local JSON graph instead.

Contract:
    graph_id is a filesystem path to a JSON document shaped as either
        {"nodes": [...], "edges": [...]}
    or a bare list of nodes (edges default to []).
    Node keys: uuid, name, labels, summary, attributes.
    Edge keys: source_node_uuid, target_node_uuid, name, fact.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("mirofish.local_graph")


@dataclass
class EntityNode:
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    related_edges: List[Dict[str, Any]] = field(default_factory=list)
    related_nodes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
            "related_edges": self.related_edges,
            "related_nodes": self.related_nodes,
        }

    def get_entity_type(self) -> Optional[str]:
        for label in self.labels:
            if label not in ("Entity", "Node"):
                return label
        return None


@dataclass
class FilteredEntities:
    entities: List[EntityNode]
    entity_types: Set[str]
    total_count: int
    filtered_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "entity_types": list(self.entity_types),
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
        }


class LocalEntityReader:
    """Read and filter entities from a local JSON graph."""

    def _load(self, graph_id: str) -> Dict[str, Any]:
        with open(graph_id, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, list):
            return {"nodes": payload, "edges": []}
        if not isinstance(payload, dict):
            raise ValueError("Local graph must be a JSON object or list")
        return {"nodes": payload.get("nodes", []), "edges": payload.get("edges", [])}

    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        nodes = []
        for node in self._load(graph_id)["nodes"]:
            nodes.append(
                {
                    "uuid": node.get("uuid", ""),
                    "name": node.get("name", ""),
                    "labels": node.get("labels", []),
                    "summary": node.get("summary", ""),
                    "attributes": node.get("attributes", {}),
                }
            )
        logger.info("Loaded %d local graph nodes from %s", len(nodes), graph_id)
        return nodes

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        return self._load(graph_id)["edges"]

    def filter_defined_entities(
        self,
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True,
    ) -> FilteredEntities:
        all_nodes = self.get_all_nodes(graph_id)
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        node_map = {n["uuid"]: n for n in all_nodes}

        filtered: List[EntityNode] = []
        entity_types_found: Set[str] = set()

        for node in all_nodes:
            labels = node.get("labels", [])
            custom_labels = [l for l in labels if l not in ("Entity", "Node")]
            if not custom_labels:
                continue
            if defined_entity_types:
                matching = [l for l in custom_labels if l in defined_entity_types]
                if not matching:
                    continue
                entity_type = matching[0]
            else:
                entity_type = custom_labels[0]

            entity_types_found.add(entity_type)
            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )

            if enrich_with_edges:
                related_edges = []
                related_uuids = set()
                for edge in all_edges:
                    if edge.get("source_node_uuid") == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "outgoing",
                                "edge_name": edge.get("name", ""),
                                "fact": edge.get("fact", ""),
                                "target_node_uuid": edge.get("target_node_uuid", ""),
                            }
                        )
                        related_uuids.add(edge.get("target_node_uuid", ""))
                    elif edge.get("target_node_uuid") == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "incoming",
                                "edge_name": edge.get("name", ""),
                                "fact": edge.get("fact", ""),
                                "source_node_uuid": edge.get("source_node_uuid", ""),
                            }
                        )
                        related_uuids.add(edge.get("source_node_uuid", ""))

                entity.related_edges = related_edges
                entity.related_nodes = [
                    {
                        "uuid": node_map[uuid]["uuid"],
                        "name": node_map[uuid]["name"],
                        "labels": node_map[uuid]["labels"],
                        "summary": node_map[uuid].get("summary", ""),
                    }
                    for uuid in related_uuids
                    if uuid in node_map
                ]

            filtered.append(entity)

        logger.info(
            "Filtered %d/%d local entities, types=%s",
            len(filtered),
            len(all_nodes),
            sorted(entity_types_found),
        )
        return FilteredEntities(
            entities=filtered,
            entity_types=entity_types_found,
            total_count=len(all_nodes),
            filtered_count=len(filtered),
        )
